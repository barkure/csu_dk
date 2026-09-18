"""打卡引擎：登录态维护 + 执行一次打卡。"""
from __future__ import annotations

import re
import threading
import time
from datetime import timedelta

import requests

from . import buildings, db, exits
from . import config as cfg
from .clock import local_now, parse_local, to_local_iso
from .crypto import decrypt_secret
from .csu.cas import CasIpFrozenError
from .csu.cas import classify_error as classify_cas_error
from .csu.zhxg import ZhxgClient, ZhxgError
from .domain import AccountRow, AuthError, CheckinResult, CheckinStatus, Trigger
from .errors import (
    LoginPausedError,
    MasterKeyInvalidError,
    MasterKeyMissingError,
    RateLimitError,
    SecretDecryptError,
)
from .locks import lock_for
from .log import log_account_enabled_changed, log_event
from .mailer import send_credential_invalid_notice
from .ratelimit import FailureCooldown, SlidingWindow

_login_paused_until = 0.0
_cas_gate = threading.Semaphore(cfg.config.cas_concurrency)
_credential_failures = FailureCooldown(cfg.config.cred_fail_max,
                                       cfg.config.cred_fail_cooldown_seconds)
_user_failures = FailureCooldown(cfg.config.cred_fail_max_user,
                                 cfg.config.cred_fail_cooldown_seconds)
_ip_failures = FailureCooldown(cfg.config.cred_fail_max_ip,
                               cfg.config.cred_fail_cooldown_seconds)
_global_attempts = (SlidingWindow(60_000, cfg.config.cas_login_per_minute)
                    if cfg.config.cas_login_per_minute else None)
_attempt_gap = (SlidingWindow(cfg.config.cas_attempt_gap_seconds * 1000, 1)
                if cfg.config.cas_attempt_gap_seconds else None)

NEEDS_RECREDENTIALS = re.compile(r"密码错误|用户名或密码|未激活|锁定|验证码|无法解密")
_CREDENTIAL_ERRORS = (SecretDecryptError, MasterKeyMissingError, MasterKeyInvalidError)
OK_CODE = "200"
NO_TASK_CODE = "331"
BUSINESS_CODES = frozenset({OK_CODE, NO_TASK_CODE})

DAY_SETTLED = frozenset({CheckinStatus.SUCCESS, CheckinStatus.SKIPPED, CheckinStatus.NO_TASK})

VERIFY_RECHECK_SEC = 30.0
VERIFY_MAX_ROUNDS = 3

JITTER_ATTEMPTS = 2

ENTRY_CHECKIN = "checkin"
ENTRY_RELOGIN = "relogin"
ENTRY_REFRESH = "refresh"
ENTRY_CREATE = "create_account"
ENTRY_UPDATE = "update_account"
USER_ENTRIES = frozenset({ENTRY_CREATE, ENTRY_UPDATE})


def login_pause_remaining() -> int:
    return max(0, int(_login_paused_until - time.time()))


def login_paused_until() -> float:
    return _login_paused_until if login_pause_remaining() > 0 else 0.0


def assert_login_allowed() -> None:
    left = login_pause_remaining()
    if left > 0:
        raise LoginPausedError(
            f"学校统一身份认证风控暂停中，约 {left // 60 + 1} 分钟后自动恢复，请稍后再试", left)


def pause_logins(reason: str) -> None:
    global _login_paused_until
    _login_paused_until = time.time() + cfg.config.ip_freeze_cooldown_seconds
    print(f"[checkin] {reason}；暂停登录 {round(cfg.config.ip_freeze_cooldown_seconds / 60)} 分钟", flush=True)


def reset_login_state() -> None:
    global _login_paused_until
    _login_paused_until = 0.0
    for guard in (_credential_failures, _user_failures, _ip_failures):
        guard.reset()
    for limiter in (_global_attempts, _attempt_gap):
        if limiter is not None:
            limiter.reset()


def reset_verifications() -> None:
    db.clear_verifications()


def is_verifying(account_id: int) -> bool:
    return db.get_verification(account_id) is not None


def forget_verification(account_id: int) -> None:
    db.delete_verification(account_id)


def pending_verifications(limit: int) -> list[dict]:
    return db.due_verifications(to_local_iso(local_now(cfg.config.tz)), limit)


def sweep_login_state() -> int:
    return sum(guard.sweep() for guard in (_credential_failures, _user_failures, _ip_failures))


def credential_failure_state(username: str) -> tuple[int, float]:
    return _credential_failures.state(f"u:{username}")


def _guard_keys(username: str, user_id: int | None, ip: str | None) -> list[tuple[str, FailureCooldown]]:
    keys = [(f"u:{username}", _credential_failures)]
    if user_id is not None:
        keys.append((f"id:{user_id}", _user_failures))
    if ip:
        keys.append((f"ip:{ip}", _ip_failures))
    return keys


def _guard_blocked(username: str, user_id: int | None, ip: str | None) -> int:
    return max((guard.blocked_for(key) for key, guard in _guard_keys(username, user_id, ip)),
               default=0)


def _guard_record_failure(username: str, user_id: int | None, ip: str | None) -> int:
    counts = [guard.record_failure(key) for key, guard in _guard_keys(username, user_id, ip)]
    return counts[0]


def _guard_clear(username: str, user_id: int | None, ip: str | None) -> None:
    for key, guard in _guard_keys(username, user_id, ip):
        guard.clear(key)


def _guard_state(username: str, user_id: int | None, ip: str | None) -> tuple[int, float]:
    states = [guard.state(key) for key, guard in _guard_keys(username, user_id, ip)]
    return states[0][0], max(until for _count, until in states)


def _local_from_epoch(epoch: float) -> str:
    if epoch <= 0:
        return ""
    return to_local_iso(local_now(cfg.config.tz) + timedelta(seconds=epoch - time.time()))


def _is_credential_rejection(message: str) -> bool:
    """仅识别学校明确返回的凭据错误。"""
    return classify_cas_error(message) == "badCredentials"


def _audit_login(entry: str, result: str, *, username: str, user_id: int | None,
                 account_id: int | None, ip: str | None, fail_count: int = 0) -> None:
    fails, cooldown_until = _guard_state(username, user_id, ip)
    log_event(
        "checkin.login",
        entry=entry,
        result=result,
        user_id=user_id,
        account_id=account_id,
        ip=ip or "",
        csu_username_tail=username[-4:] if username else "",
        fail_count=fail_count or fails,
        cooldown_until=_local_from_epoch(cooldown_until),
        paused_until=_local_from_epoch(login_paused_until()),
    )


def cas_login(client: ZhxgClient, username: str, password: str | None, *,
              entry: str, user_id: int | None = None, account_id: int | None = None,
              ip: str | None = None) -> str:
    """统一处理登录暂停、凭据冷却和审计。"""
    password_login_checked = False
    failover_retry = False

    def before_password_login() -> None:
        nonlocal password_login_checked
        if password_login_checked:
            return
        remaining = login_pause_remaining()
        if remaining > 0:
            _audit_login(entry, "global_paused", username=username, user_id=user_id,
                         account_id=account_id, ip=ip)
            assert_login_allowed()

        blocked = _guard_blocked(username, user_id, ip)
        if blocked > 0:
            _audit_login(entry, "cred_cooldown", username=username, user_id=user_id,
                         account_id=account_id, ip=ip)
            raise RateLimitError(f"密码连续输错，请 {blocked} 秒后再试", blocked)

        if _global_attempts is not None:
            burst = _global_attempts.take("cas")
            if not burst.ok:
                _audit_login(entry, "login_rate_limited", username=username, user_id=user_id,
                             account_id=account_id, ip=ip)
                raise RateLimitError(f"登录请求太密集，请 {burst.retry_after_sec} 秒后再试",
                                     burst.retry_after_sec)
        if _attempt_gap is not None and entry in USER_ENTRIES and not failover_retry:
            gap = _attempt_gap.take(username)
            if not gap.ok:
                _audit_login(entry, "login_too_soon", username=username, user_id=user_id,
                             account_id=account_id, ip=ip)
                raise RateLimitError(f"刚提交过，请 {gap.retry_after_sec} 秒后再试",
                                     gap.retry_after_sec)
        password_login_checked = True

    with _cas_gate:
        while True:
            password_login_checked = False
            try:
                if not client.has_login_cookie():
                    before_password_login()
                html = client.login(username, password, before_password_login=before_password_login)
            except (LoginPausedError, RateLimitError):
                raise
            except CasIpFrozenError as error:
                switched = exits.mark_frozen(client.exit, str(error))
                if switched is None:
                    pause_logins(str(error))
                _audit_login(entry, "ip_frozen", username=username, user_id=user_id,
                             account_id=account_id, ip=ip)
                if switched is None or failover_retry:
                    raise
                client.switch_exit(switched)
                failover_retry = True
                continue
            except Exception as error:
                if _is_credential_rejection(str(error)):
                    count = _guard_record_failure(username, user_id, ip)
                    _audit_login(entry, "bad_credentials", username=username, user_id=user_id,
                                 account_id=account_id, ip=ip, fail_count=count)
                else:
                    result = classify_cas_error(str(error)) or "upstream_error"
                    _audit_login(entry, result, username=username, user_id=user_id,
                                 account_id=account_id, ip=ip)
                raise
            _guard_clear(username, user_id, ip)
            _audit_login(entry, "ok", username=username, user_id=user_id,
                         account_id=account_id, ip=ip)
            return html


def verify_login(username: str, password: str, *, entry: str = ENTRY_CREATE,
                 user_id: int | None = None, account_id: int | None = None,
                 ip: str | None = None) -> dict:
    client = ZhxgClient()
    cas_login(client, username, password, entry=entry, user_id=user_id,
              account_id=account_id, ip=ip)
    return {
        "session": {
            "token": client.token or "",
            "casual": client.casual,
            "cookies": client.cookies_json(),
        },
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


_CAS_FAILURE_KINDS = {
    "badCredentials": AuthError.BAD_CREDENTIALS,
    "locked": AuthError.LOCKED,
    "captcha": AuthError.OTHER,
    "inactive": AuthError.OTHER,
}


def _failure_kind(error: Exception, message: str) -> AuthError | None:
    """返回账号认证故障；临时错误返回 None。"""
    if isinstance(error, CasIpFrozenError):
        return None
    if isinstance(error, _CREDENTIAL_ERRORS):
        return AuthError.OTHER
    if isinstance(error, ZhxgError):
        return AuthError.OTHER
    return _CAS_FAILURE_KINDS.get(classify_cas_error(message) or "")


_SECRET_KEYS = r"CASTGC|JSESSIONID|authorization|token|password|passwd|pwd|cookies?|secret|casual"
_QUOTABLE_KEYS = rf"(?:{_SECRET_KEYS}|set-cookie)"
_SECRET_IN_TEXT = re.compile(
    r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"
    r"|[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}"
    r"|v1\.[A-Za-z0-9+/=_\-.]{8,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]+"
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*\"(?:[^\"\\]|\\.)*\""
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*'(?:[^'\\]|\\.)*'"
    r"|[\"']?(?:set-cookie|castgc|jsessionid|cookies?|authorization)[\"']?\s*[=:]\s*[^,，。)\n]+"
    rf"|[\"']?(?:{_SECRET_KEYS})[\"']?\s*[=:]\s*[\"']?[^\s\"'&,，。;；)\n]+"
    r"|(?<=[?&])[A-Za-z0-9_]+=[^\s&,，。;；]+",
    re.IGNORECASE,
)
_DETAIL_LIMIT = 160


def scrub_detail(detail: str) -> str:
    return _SECRET_IN_TEXT.sub("***", str(detail or ""))[:_DETAIL_LIMIT]


def mark_auth_failure(account_id: int, error: Exception, message: str) -> AuthError | None:
    """记录账号认证故障。"""
    kind = _failure_kind(error, message)
    if kind:
        target = db.get_account_notification_target(account_id)
        db.set_auth_error(account_id, kind)
        log_event("checkin.auth_failed", account_id=account_id, kind=kind,
                  detail=scrub_detail(message))
        if kind == AuthError.BAD_CREDENTIALS and target and target["auth_error"] != kind:
            try:
                result = send_credential_invalid_notice(target["email"], target["csu_username"])
                log_event("account.credential_invalid_notified", account_id=account_id, sent=result["sent"])
            except Exception as notify_error:  # noqa: BLE001 - 通知失败不影响账号状态
                log_event("account.credential_invalid_notify_failed", level="warning", account_id=account_id,
                          error=scrub_detail(str(notify_error)))
    return kind


def has_fresh_login(account: dict) -> bool:
    """判断登录态是否属于今天。"""
    if not account.get("token") or not account.get("token_at"):
        return False
    now = local_now(cfg.config.tz)
    issued = parse_local(account["token_at"])
    return issued <= now and account["token_at"][:10] == to_local_iso(now)[:10]


def persist_state(account_id: int, client: ZhxgClient) -> None:
    db.update_account(account_id, {
        "token": client.token or "",
        "casual": client.casual,
        "cookies": client.cookies_json(),
        "token_at": to_local_iso(local_now(cfg.config.tz)),
        "auth_error": "",
    })


def _login(account: dict, *, entry: str = ENTRY_CHECKIN, ip: str | None = None) -> ZhxgClient:
    client = build_client(account)
    try:
        password = _decrypt_password(account)
    except SecretDecryptError:
        password = None
    cas_login(client, account["csu_username"], password, entry=entry,
              user_id=account.get("user_id"), account_id=account.get("id"), ip=ip)
    persist_state(account["id"], client)
    return client


def _locked(account: dict, fn):
    """拿锁后重新读账号：排队期间前一个任务可能刚写回新登录态。"""
    with lock_for(f"account:{account['id']}"):
        return fn(db.get_account_by_id(account["id"]) or account)


_relogin_limiter = SlidingWindow(cfg.config.relogin_cooldown_seconds * 1000, 1)


def relogin(account: dict, *, ip: str | None = None) -> dict:
    verdict = _relogin_limiter.take(f"account:{account['id']}")
    if not verdict.ok:
        raise RateLimitError(f"刚上过号，请 {verdict.retry_after_sec} 秒后再试", verdict.retry_after_sec)
    return _locked(account, lambda fresh: _relogin_body(fresh, ip=ip))


def _relogin_body(account: dict, *, ip: str | None = None) -> dict:
    """已持有账号锁：只上号，不打卡。"""
    try:
        _login(account, entry=ENTRY_RELOGIN, ip=ip)
        return {"ok": True, "message": "已重新上号，登录态已更新"}
    except (LoginPausedError, RateLimitError):
        raise
    except Exception as error:  # noqa: BLE001 - 任何失败都要回给界面，不能抛出去
        message = str(error)
        mark_auth_failure(account["id"], error, message)
        return {"ok": False, "message": message}


def refresh_login(account: dict) -> dict:
    """调度刷新当天登录态，不打卡。"""
    return _locked(account, _refresh_login_body)


def _refresh_login_body(account: dict) -> dict:
    try:
        _login(account, entry=ENTRY_REFRESH)
        return {"ok": True, "message": "已刷新登录态"}
    except (LoginPausedError, RateLimitError) as error:
        return {"ok": False, "deferred": True, "message": str(error)}
    except Exception as error:  # noqa: BLE001 - 刷新失败只记账号故障，不写成打卡记录
        message = str(error)
        mark_auth_failure(account["id"], error, message)
        return {"ok": False, "message": message}


def _paused_result(error: Exception) -> CheckinResult:
    until = login_paused_until() or (time.time() + cfg.config.ip_freeze_cooldown_seconds)
    return {"status": CheckinStatus.FAILED, "message": str(error), "dksj": None,
            "paused_until": until}


def run_checkin(account: AccountRow | dict, trigger: Trigger | str = Trigger.SCHEDULE) -> CheckinResult:
    if is_verifying(account["id"]):
        return _verifying_result()
    return _locked(account, lambda fresh: _run(fresh, trigger))


def _coords_for(account: dict) -> tuple[float, float] | None:
    name = account.get("dkdz") or ""
    if buildings.cacheable(name):
        return buildings.resolve(name)
    if account.get("jd") is None or account.get("wd") is None:
        return None
    return float(account["jd"]), float(account["wd"])


def _save_location(account: dict, name: str, coord: tuple[float, float] | None = None) -> None:
    name = (name or "").strip()
    fields: dict = {}
    if name and name != account.get("dkdz"):
        fields["dkdz"] = name
        account["dkdz"] = name
    if coord is not None:
        if buildings.cacheable(name or account.get("dkdz") or ""):
            if account.get("jd") is not None or account.get("wd") is not None:
                fields["jd"] = None
                fields["wd"] = None
                account["jd"] = account["wd"] = None
        elif (account.get("jd"), account.get("wd")) != (coord[0], coord[1]):
            fields["jd"] = coord[0]
            fields["wd"] = coord[1]
            account["jd"], account["wd"] = coord
    if not fields:
        return
    fields["updated_at"] = to_local_iso(local_now(cfg.config.tz))
    db.update_account(account["id"], fields)


def _determine_location(client, account) -> tuple[float, float, dict] | None:
    """测定可用坐标：宿舍写入楼栋缓存，租房写入账号。"""
    previous = account.get("dkdz") or "未测"
    try:
        coord, school_name, verdict, source = buildings.for_student(client, account.get("dkdz") or "")
    except Exception:  # noqa: BLE001 - 重测只是补救，失败不能掩盖原本的打卡结果
        return None
    if not verdict.get("canDk"):
        return None
    _save_location(account, school_name, coord)
    if source == "located":
        log_event("checkin.relocated", level="warning", account_id=account["id"],
                  user_id=account.get("user_id"), building=school_name, previous=previous)
    return coord[0], coord[1], verdict


def _jitter_coord(client: ZhxgClient, account: dict,
                  coord: tuple[float, float]) -> tuple[float, float]:
    radius = cfg.config.checkin_jitter_meters
    if radius <= 0:
        return coord
    for _ in range(JITTER_ATTEMPTS):
        point = buildings.scatter(coord, radius)
        try:
            verdict = buildings.check(client, point)
        except Exception:  # noqa: BLE001 - 失败时使用原坐标
            continue
        if verdict.get("canDk"):
            return point
    log_event("checkin.jitter_fallback", level="warning", account_id=account["id"],
              radius_m=radius)
    return coord


def _read_submit_result(client: ZhxgClient, account_id: int) -> tuple[bool, str | None]:
    try:
        response = client.dk_status()
    except Exception as error:  # noqa: BLE001 - 查询失败不代表打卡失败
        log_event("checkin.verify_read_failed", level="warning", account_id=account_id,
                  detail=scrub_detail(str(error)))
        return False, None
    code = str(response.get("code") or "")
    if code != OK_CODE:
        log_event("checkin.verify_read_failed", level="warning", account_id=account_id,
                  upstream_status=code, detail=scrub_detail(response.get("message") or ""))
        return False, None
    data = response.get("data") or {}
    if data.get("sfydk"):
        return True, data.get("dksj")
    return False, None


def _submit(client: ZhxgClient, account: dict, data: dict) -> tuple[CheckinStatus, str, str | None] | None:
    if data.get("sfydk"):
        dksj = data.get("dksj")
        return CheckinStatus.SKIPPED, f"今日已打卡（{dksj or '—'}）", dksj
    if not data.get("kdk"):
        reason = data.get("bkyy") or "未知原因"
        status = CheckinStatus.WAITING if "未到" in str(reason) else CheckinStatus.FAILED
        return status, f"当前不可打卡：{reason}", None

    cached = _coords_for(account)
    location = (client.check_location(*cached).get("data") or {}) if cached else {}
    coord = cached if cached and location.get("canDk") else None
    if coord is None:
        measured = _determine_location(client, account)
        if measured:
            coord, location = (measured[0], measured[1]), measured[2]
    _save_location(account, location.get("yxMc") or "", coord)
    if coord is None or not location.get("canDk"):
        message = (f"位置校验未通过：{location.get('msg') or location.get('reason') or '—'}"
                   f"（距 {location.get('yxMc') or '?'} {location.get('pcMi') or '?'} 米）")
        return CheckinStatus.FAILED, message, None

    jd, wd = _jitter_coord(client, account, coord)
    try:
        result = client.submit_dk(jd=jd, wd=wd, dkbc=data.get("dkbc") or "",
                                  dkdz=location.get("yxMc") or account.get("dkdz") or "")
    except (requests.RequestException, TimeoutError, ConnectionError) as error:
        log_event("checkin.submit_unknown", level="warning", account_id=account["id"],
                  detail=scrub_detail(str(error)))
        return None
    if result.get("code") != OK_CODE:
        raise RuntimeError(f"提交失败：{result.get('message') or str(result)[:160]}")
    confirmed, dksj = _read_submit_result(client, account["id"])
    if confirmed:
        return CheckinStatus.SUCCESS, f"打卡成功（{dksj}）", dksj
    return None


def _business_status(account: dict) -> tuple[ZhxgClient, dict, str]:
    client = build_client(account)
    response: dict = {}
    for force in (False, True):
        if force or not has_fresh_login(account) or not client.token:
            try:
                client = _login(account, entry=ENTRY_CHECKIN)
            except (LoginPausedError, CasIpFrozenError, RateLimitError):
                raise
            except Exception as error:
                mark_auth_failure(account["id"], error, str(error))
                raise
        response = client.dk_status()
        code = str(response.get("code") or "")
        if code in BUSINESS_CODES:
            return client, response, code
    return client, response, str(response.get("code") or "")


def _settled_today(account: dict, run_at: str) -> bool:
    return (str(account.get("last_run_at") or "")[:10] == run_at[:10]
            and account.get("last_status") in DAY_SETTLED)


def _record(account: dict, run_at: str, trigger: str, status: CheckinStatus,
            message: str, dksj: str | None) -> None:
    db.add_record(account["id"], run_at, trigger, status, message, dksj)
    fields = {"last_run_at": run_at, "last_status": status, "last_message": message}
    if status == CheckinStatus.FAILED and _settled_today(account, run_at):
        fields = {}
    if status == CheckinStatus.NO_TASK:
        fields["enabled"] = 0
    db.update_account(account["id"], fields)
    if status == CheckinStatus.NO_TASK:
        log_account_enabled_changed(user_id=account["user_id"], account_id=account["id"],
                                    username=account.get("csu_username") or "",
                                    enabled=False, source="school_no_task")


def _verifying_result() -> CheckinResult:
    return {"status": CheckinStatus.WAITING, "message": "打卡结果确认中，请稍候",
            "dksj": None, "deferred": True}


def _recheck_at(after_seconds: float) -> str:
    return to_local_iso(local_now(cfg.config.tz) + timedelta(seconds=after_seconds))


def _defer_verification(account: dict, run_at: str, trigger: str) -> CheckinResult:
    db.upsert_verification(account["id"], run_at, trigger,
                           _recheck_at(VERIFY_RECHECK_SEC), run_at)
    log_event("checkin.verify_deferred", level="warning", account_id=account["id"],
              round=1, due_in=VERIFY_RECHECK_SEC)
    return {"status": CheckinStatus.WAITING, "message": "已提交打卡，等待学校确认",
            "dksj": None, "deferred": True}


def resolve_verification(account: AccountRow | dict) -> CheckinResult | None:
    return _locked(account, _resolve_verification_body)


def _resolve_verification_body(account: dict) -> CheckinResult | None:
    task = db.get_verification(account["id"])
    if task is None:
        return None
    run_at = str(task.get("run_at") or "")
    today = to_local_iso(local_now(cfg.config.tz))[:10]
    if run_at[:10] != today:
        db.delete_verification(account["id"])
        log_event("checkin.verify_abandoned", level="warning", account_id=account["id"],
                  run_at=run_at)
        return None

    client = build_client(account)
    if not client.token or not has_fresh_login(account):
        forget_verification(account["id"])
        return None

    detail = ""
    try:
        response = client.dk_status()
    except Exception as error:  # noqa: BLE001 - 查询失败不代表打卡失败
        response, detail = {}, scrub_detail(str(error))
    code = str(response.get("code") or "")
    if code and code != OK_CODE:
        detail = scrub_detail(response.get("message") or f"code={code}")
    data = response.get("data") or {}
    rounds = int(task.get("rounds") or 0) + 1

    if code == OK_CODE and data.get("sfydk"):
        dksj = data.get("dksj")
        status, message = CheckinStatus.SUCCESS, f"打卡成功（{dksj}）"
    elif rounds >= VERIFY_MAX_ROUNDS:
        dksj = data.get("dksj")
        status, message = CheckinStatus.FAILED, "提交返回成功但复核未通过，请手动确认"
    else:
        db.bump_verification(account["id"], _recheck_at(VERIFY_RECHECK_SEC))
        log_event("checkin.verify_deferred", level="warning", account_id=account["id"],
                  round=rounds + 1, due_in=VERIFY_RECHECK_SEC, detail=detail)
        return None

    with db.transaction():
        _record(account, run_at, task["trigger"], status, message, dksj)
        forget_verification(account["id"])
    log_event("checkin.verify_resolved", account_id=account["id"], status=status,
              rounds=rounds, detail=detail)
    return {"status": status, "message": message, "dksj": dksj}


def _run(account: AccountRow | dict, trigger: str) -> CheckinResult:
    if is_verifying(account["id"]):
        return _verifying_result()

    run_at = to_local_iso(local_now(cfg.config.tz))
    status, message, dksj = CheckinStatus.FAILED, "", None

    try:
        if not account.get("enabled"):
            raise RuntimeError("账号已停用")

        client, response, code = _business_status(account)
        if code == NO_TASK_CODE:
            status = CheckinStatus.NO_TASK
            message = "无打卡事项"
        elif code != OK_CODE:
            raise RuntimeError(f"业务接口返回异常：{str(response)[:160]}")
        else:
            persist_state(account["id"], client)
            outcome = _submit(client, account, response.get("data") or {})
            if outcome is None:
                return _defer_verification(account, run_at, trigger)
            status, message, dksj = outcome
    except (LoginPausedError, CasIpFrozenError) as error:
        return _paused_result(error)
    except RateLimitError as error:
        if trigger == Trigger.SCHEDULE:
            return {"status": CheckinStatus.WAITING, "message": str(error), "dksj": None,
                    "deferred": True}
        message = str(error)
    except Exception as error:  # noqa: BLE001 - 打卡失败要落库并展示，不能中断整轮调度
        message = str(error)

    _record(account, run_at, trigger, status, message, dksj)
    return {"status": status, "message": message, "dksj": dksj}
