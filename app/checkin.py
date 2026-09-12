"""打卡引擎：登录态维护 + 执行一次打卡。"""
from __future__ import annotations

import re
import threading
import time

from . import config as cfg
from . import db
from .clock import local_now, parse_local, to_local_iso
from .crypto import decrypt_secret
from .csu.cas import CasIpFrozenError
from .csu.cas import classify_error as classify_cas_error
from .csu.zhxg import ZhxgClient, ZhxgError
from .domain import AccountRow, AuthError, CheckinResult, CheckinStatus, Trigger
from .errors import MasterKeyInvalidError, MasterKeyMissingError, RateLimitError, SecretDecryptError
from .locks import lock_for
from .log import log_event
from .ratelimit import SlidingWindow
from .validate import apply_window_margin, parse_window_text

_login_paused_until = 0.0
# 学校风控按出口 IP 判定，所以密码登录全局串行（班次/位置查询不经过这里）
_cas_gate = threading.Semaphore(cfg.config.cas_concurrency)

NEEDS_RECREDENTIALS = re.compile(r"密码错误|用户名或密码|未激活|锁定|验证码|无法解密")
_CREDENTIAL_ERRORS = (SecretDecryptError, MasterKeyMissingError, MasterKeyInvalidError)


def assert_login_allowed() -> None:
    left = _login_paused_until - time.time()
    if left > 0:
        raise RuntimeError(f"登录已暂停约 {int(left // 60) + 1} 分钟：学校风控冻结了本机 IP，稍后会自动恢复")


def pause_logins(reason: str) -> None:
    global _login_paused_until
    _login_paused_until = time.time() + cfg.config.ip_freeze_cooldown_seconds
    print(f"[checkin] {reason}；暂停登录 {round(cfg.config.ip_freeze_cooldown_seconds / 60)} 分钟", flush=True)


def cas_login(client: ZhxgClient, username: str, password: str | None) -> str:
    assert_login_allowed()
    with _cas_gate:
        assert_login_allowed()
        try:
            return client.login(username, password)
        except CasIpFrozenError as error:
            pause_logins(str(error))
            raise


def probe_window(username: str, password: str, jd: float | None = None, wd: float | None = None) -> dict:
    """登录学校读一次班次/窗口/定位（不落库、不改状态）。"""
    client = ZhxgClient()
    cas_login(client, username, password)

    response = client.dk_status()
    if response.get("code") != "200":
        raise RuntimeError(f"接口返回异常：{str(response)[:160]}")
    data = response.get("data") or {}

    location = None
    if isinstance(jd, (int, float)) and isinstance(wd, (int, float)):
        checked = client.check_location(jd, wd)
        if checked.get("code") == "200":
            location = checked.get("data") or {}

    allowed = parse_window_text(data.get("dksjfw"))
    return {
        "location": location,
        # 只用这一次实时请求的结果：不在打卡时段内时学校不按坐标给楼栋名，那就先空着
        "address": (location or {}).get("yxMc"),
        # 本次验证已经登录成功，把登录态带回落库，免得卡片显示未登录、当晚再登一次
        "session": {
            "token": client.token or "",
            "casual": client.casual,
            "cookies": client.cookies_json(),
        },
        "dkbc": data.get("dkbc") or "",
        "allowedWindow": allowed,
        "window": apply_window_margin(allowed, cfg.config.window_margin_minutes),
        "dksjfw": data.get("dksjfw"),
        "marginMinutes": cfg.config.window_margin_minutes,
        "kdk": bool(data.get("kdk")),
        "sfydk": bool(data.get("sfydk")),
        "dksj": data.get("dksj"),
        "bkyy": data.get("bkyy"),
    }


def build_client(account: dict) -> ZhxgClient:
    return ZhxgClient(casual=account.get("casual"), token=account.get("token"), cookies=account.get("cookies"))


def _decrypt_password(account: dict) -> str:
    try:
        return decrypt_secret(account["password_enc"])
    except _CREDENTIAL_ERRORS as error:
        raise SecretDecryptError(
            "本地保存的密码无法解密（加密密钥不匹配或数据损坏），请在「编辑账号」里重新提交一次密码",
        ) from error


def fill_address(account: dict) -> str | None:
    """补楼栋名。

    学校只在**打卡时段内**按坐标返回楼栋名（时段外它直接回"未到/已过打卡时间"，连距离都不给），
    所以时段外调用是白调；而且只用**现成的登录态**，不为这一件事单独登录一次学校。
    拿不到就返回 None（宁可不显示，也不拿历史记录凑数）。
    """
    if account.get("dkdz") or account.get("jd") is None or account.get("wd") is None:
        return None
    if not has_fresh_login(account):
        return None

    address = ((build_client(account).check_location(account["jd"], account["wd"]) or {})
               .get("data") or {}).get("yxMc")
    if address:
        db.update_account(account["id"], {"dkdz": address})
        return address
    return None


# 四种账号状态里除"正常"以外的三种都由这里产出；页面只显示这个类型，详细原因进日志
_CAS_FAILURE_KINDS = {
    "badCredentials": AuthError.BAD_CREDENTIALS,   # 学校明确说用户名或密码错误
    "locked": AuthError.LOCKED,                    # 学校明确说账号被锁定
    "captcha": AuthError.OTHER,                    # 验证码识别/校验失败
    "inactive": AuthError.OTHER,                   # 未激活等其它导致认证无法继续的故障
}


def _failure_kind(error: Exception, message: str) -> AuthError | None:
    """把一次失败归到认证故障类型；None 表示"不是账号的认证故障"。

    不算故障的几类（都会自动重试，记成故障只会误导用户去改密码）：
    · 学校临时冻结出口 IP（影响的是这台机器的 IP，不是某个账号）
    · 网络超时/抖动等 requests 异常
    · 会话失效（sessionExpired）—— 重新登录即可，属于正常流程
    · 打卡阶段的问题（位置校验、提交失败等），由调用方按"仅登录阶段"过滤
    """
    if isinstance(error, CasIpFrozenError):
        return None
    if isinstance(error, _CREDENTIAL_ERRORS):
        return AuthError.OTHER              # 本地密钥缺失/不匹配、密文损坏
    if isinstance(error, ZhxgError):
        return AuthError.OTHER              # 认证链路断了（换业务 token 失败等）
    return _CAS_FAILURE_KINDS.get(classify_cas_error(message) or "")


# 日志的脱敏规则只看"键名"，不看值：异常文本里可能夹着 token / cookie / JWT，
# 所以这里按"长得像密钥"的形态擦掉，再截断长度。
_SECRET_KEYS = r"CASTGC|JSESSIONID|authorization|token|password|passwd|pwd|cookies?|secret|casual"
# 值可能被引号包起来（JSON / Set-Cookie 都有这种形态）
_QUOTABLE_KEYS = rf"(?:{_SECRET_KEYS}|set-cookie)"
_SECRET_IN_TEXT = re.compile(
    # 规则按"最具体 → 最宽泛"排列：先来的先匹配，排错顺序就会出现"擦一半、留一半"
    r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"      # JWT（base64 的 {" 固定以 eyJ 开头）
    r"|[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}"    # 其它三段式凭据
    r"|v1\.[A-Za-z0-9+/=_\-.]{8,}"                                        # 本地密文
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]+"                                     # Authorization: Bearer xxx
    # 带引号的值整串吃掉（可含空格、逗号、右括号、转义），必须排在宽泛规则之前
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*\"(?:[^\"\\]|\\.)*\""
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*'(?:[^'\\]|\\.)*'"
    # Cookie/请求头：没引号时值可能带空格与分号（a=b; c=d），吃到逗号/右括号/换行为止
    r"|[\"']?(?:set-cookie|castgc|jsessionid|cookies?|authorization)[\"']?\s*[=:]\s*[^,，。)\n]+"
    # 其余单值键：值是没引号的单段
    rf"|[\"']?(?:{_SECRET_KEYS})[\"']?\s*[=:]\s*[\"']?[^\s\"'&,，。;；)\n]+"
    r"|(?<=[?&])[A-Za-z0-9_]+=[^\s&,，。;；]+",                             # URL 查询串里的值
    re.IGNORECASE,
)
_DETAIL_LIMIT = 160


def scrub_detail(detail: str) -> str:
    return _SECRET_IN_TEXT.sub("***", str(detail or ""))[:_DETAIL_LIMIT]


def mark_auth_failure(account_id: int, error: Exception, message: str) -> AuthError | None:
    """记录账号的认证故障（页面四种状态的唯一依据），返回记录的类型。

    详情只进服务日志：截断后写入，且不包含密码、完整 Token 或 Cookie。
    """
    kind = _failure_kind(error, message)
    if kind:
        db.set_auth_error(account_id, kind)
        log_event("checkin.auth_failed", account_id=account_id, kind=kind,
                  detail=scrub_detail(message))
    return kind


def has_fresh_login(account: dict) -> bool:
    """判据是同一自然日（学校 JWT 当天 24:00 到期），token_ttl_seconds 只是兜底上限。"""
    if not account.get("token") or not account.get("token_at"):
        return False
    now = local_now(cfg.config.tz)
    issued = parse_local(account["token_at"])
    if issued > now:
        return False
    if account["token_at"][:10] != to_local_iso(now)[:10]:
        return False
    return (now - issued).total_seconds() < cfg.config.token_ttl_seconds


def persist_state(account_id: int, client: ZhxgClient) -> None:
    db.update_account(account_id, {
        "token": client.token or "",
        "casual": client.casual,
        "cookies": client.cookies_json(),
        "token_at": to_local_iso(local_now(cfg.config.tz)),
        # 认证成功就清除原有的认证故障（页面恢复"正常"）
        "needs_reauth": 0,
        "auth_error": "",
    })


def _login(account: dict) -> ZhxgClient:
    client = build_client(account)
    try:
        password = _decrypt_password(account)
    except SecretDecryptError:
        # 密码解不开不等于账号废了：CAS 会话还有效时照样能换 ticket，
        # 真正需要交密码时 cas_login 会报错，那时才轮到人工重填。
        password = None
    cas_login(client, account["csu_username"], password)
    persist_state(account["id"], client)
    return client


def _locked(account: dict, fn):
    """拿锁后重新读账号：排队期间前一个任务可能刚写回新登录态。"""
    with lock_for(f"account:{account['id']}"):
        return fn(db.get_account_by_id(account["id"]) or account)


# 「重新上号」每次都真的登录一次学校，所以按账号限流（默认 60 秒一次）。
# 放在服务层：网页与 JSON 接口共用同一把闸门，不能只在某一条路由上做。
_relogin_limiter = SlidingWindow(cfg.config.relogin_cooldown_seconds * 1000, 1)


def relogin(account: dict) -> dict:
    verdict = _relogin_limiter.take(f"account:{account['id']}")
    if not verdict.ok:
        raise RateLimitError(f"刚上过号，请 {verdict.retry_after_sec} 秒后再试", verdict.retry_after_sec)
    return _locked(account, _relogin_body)


def _relogin_body(account: dict) -> dict:
    """已持有账号锁：只上号，不打卡。"""
    try:
        _login(account)
        return {"ok": True, "message": "已重新上号，登录态已更新"}
    except Exception as error:  # noqa: BLE001 - 任何失败都要回给界面，不能抛出去
        message = str(error)
        mark_auth_failure(account["id"], error, message)
        return {"ok": False, "message": message}


def run_checkin(account: AccountRow | dict, trigger: Trigger | str = Trigger.SCHEDULE) -> CheckinResult:
    return _locked(account, lambda fresh: _run(fresh, trigger))


def _run(account: AccountRow | dict, trigger: str) -> CheckinResult:
    run_at = to_local_iso(local_now(cfg.config.tz))
    status, message, dksj = CheckinStatus.FAILED, "", None
    # 只有"正在登录"那几行的失败才可能改变账号状态。默认算打卡阶段：
    # 停用、班次查询、位置校验、提交这些环节出的问题一律只留在执行记录里。
    stage = "checkin"

    try:
        if not account.get("enabled"):
            raise RuntimeError("账号已停用")

        client = build_client(account)
        response: dict = {}
        for force in (False, True):
            if force or not has_fresh_login(account) or not client.token:
                stage = "login"
                client = _login(account)
                stage = "checkin"
            response = client.dk_status()
            if response.get("code") == "200":
                break
        if response.get("code") != "200":
            raise RuntimeError(f"业务接口返回异常：{str(response)[:160]}")

        persist_state(account["id"], client)
        data = response.get("data") or {}

        if data.get("sfydk"):
            status = CheckinStatus.SKIPPED
            dksj = data.get("dksj")
            message = f"今日已打卡（{dksj or '—'}）"
        elif not data.get("kdk"):
            message = f"当前不可打卡：{data.get('bkyy') or '未知原因'}"
            if "未到" in str(data.get("bkyy") or ""):
                status = CheckinStatus.WAITING
        else:
            location = (client.check_location(account["jd"], account["wd"]).get("data") or {})
            if not account.get("dkdz") and location.get("yxMc"):
                db.update_account(account["id"], {"dkdz": location["yxMc"]})

            if not location.get("canDk"):
                message = (
                    f"位置校验未通过：{location.get('msg') or location.get('reason') or '—'}"
                    f"（距 {location.get('yxMc') or '?'} {location.get('pcMi') or '?'} 米）"
                )
            else:
                result = client.submit_dk(
                    jd=account["jd"],
                    wd=account["wd"],
                    dkbc=data.get("dkbc") or "",
                    dkdz=location.get("yxMc") or account.get("dkdz") or "",
                )
                if result.get("code") != "200":
                    raise RuntimeError(f"提交失败：{result.get('message') or str(result)[:160]}")
                after = client.dk_status().get("data") or {}
                dksj = after.get("dksj")
                status = CheckinStatus.SUCCESS if after.get("sfydk") else CheckinStatus.FAILED
                message = (f"打卡成功（{dksj}）" if status == CheckinStatus.SUCCESS
                           else "提交返回成功但复核未通过，请手动确认")
    except Exception as error:  # noqa: BLE001 - 打卡失败要落库并展示，不能中断整轮调度
        message = str(error)
        if stage == "login":
            mark_auth_failure(account["id"], error, message)

    db.add_record(account["id"], run_at, trigger, status, message, dksj)
    db.update_account(account["id"], {"last_run_at": run_at, "last_status": status, "last_message": message})
    return {"status": status, "message": message, "dksj": dksj}
