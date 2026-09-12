"""账号的新增/编辑（"提交即验证"的那套流程）。JSON API 与网页表单共用这一份。"""
from __future__ import annotations

from . import config as cfg
from . import db
from .checkin import mark_auth_failure, probe_window
from .clock import local_now, to_local_iso
from .crypto import decrypt_secret, encrypt_secret
from .errors import AppError, BadRequestError
from .locks import lock_for
from .validate import validate_coordinates, validate_window


def _now_iso() -> str:
    return to_local_iso(local_now(cfg.config.tz))


def _read_coords(payload: dict, existing: dict | None) -> tuple[float, float]:
    try:
        jd = existing["jd"] if payload.get("jd") is None else float(payload["jd"])
        wd = existing["wd"] if payload.get("wd") is None else float(payload["wd"])
    except (TypeError, ValueError) as error:
        raise BadRequestError("经纬度必须是有效数字", "invalid_coord") from error
    if jd is None or wd is None:
        raise BadRequestError("缺少经纬度：请填写，或点「使用当前定位」自动获取", "invalid_coord")
    return validate_coordinates(jd, wd)


def _probe(username: str, password: str | None, existing: dict | None, jd: float, wd: float) -> dict | None:
    """提交即验证：登录学校读一次班次/窗口/定位，读不通就不保存。"""
    candidate = password or (decrypt_secret(existing["password_enc"]) if existing else None)
    if not candidate:
        return None
    try:
        return probe_window(username, candidate, jd, wd)
    except Exception as error:
        raise AppError(f"验证失败，未保存：{error}", status=400, expose=True) from error


def create_or_update(user: dict, payload: dict) -> dict:
    """新增/编辑账号。**按用户串行**（不只是按学号）：

    · 避免并发的新增请求都去登录一次学校（学校按出口 IP 风控）；
    · 避免两个不同学号同时通过"数量没超上限"的检查 —— 先查数量再插入会穿透；
    · 网页与 JSON 接口共用这一把锁，免得只在某一条路由上加保护。
    """
    with lock_for(f"add:{user['id']}"):
        return _create_or_update_locked(user, payload)


def _create_or_update_locked(user: dict, payload: dict) -> dict:
    csu_username = str(payload.get("csuUsername") or "").strip()
    if not csu_username:
        raise BadRequestError("缺少学号")

    existing = db.get_account_by_username(user["id"], csu_username)
    password = str(payload["password"]) if payload.get("password") else None
    if not password and not existing:
        raise BadRequestError("请填写密码（服务端不接受明文）")
    if not existing and db.count_accounts(user["id"]) >= cfg.config.max_accounts_per_user:
        raise BadRequestError(f"最多只能托管 {cfg.config.max_accounts_per_user} 个账号，请先删除不用的")

    jd, wd = _read_coords(payload, existing)
    try:
        probe = _probe(csu_username, password, existing, jd, wd)
    except AppError as error:
        # 编辑已有账号时验证失败：按原因记下账号状态（新建的还没落库，无从记录）
        if existing:
            mark_auth_failure(existing["id"], error.__cause__ or error, error.message)
        raise

    window = (probe or {}).get("window") or (None, None)
    window_start = str(payload.get("windowStart") or window[0] or cfg.config.default_window_start)
    window_end = str(payload.get("windowEnd") or window[1] or cfg.config.default_window_end)
    validate_window(window_start, window_end, cfg.config.max_window_hours)

    fields = {
        # 不传 enabled 时：编辑保持原状，新建默认开启（漏掉新建分支会直接 TypeError）
        "enabled": (existing["enabled"] if existing else 1) if payload.get("enabled") is None
        else (0 if payload["enabled"] is False else 1),
        "window_start": window_start,
        "window_end": window_end,
        "jd": jd,
        "wd": wd,
        # 地址只认这一次实时请求拿到的楼栋名；拿不到就留空
        "dkdz": (probe or {}).get("address") or (existing or {}).get("dkdz") or "",
        "updated_at": _now_iso(),
    }

    session = {}
    if (probe or {}).get("session", {}).get("token"):
        session = {
            "token": probe["session"]["token"],
            "casual": probe["session"]["casual"],
            "cookies": probe["session"]["cookies"],
            "token_at": _now_iso(),
            "needs_reauth": 0,
            "auth_error": "",       # 这次验证真的登录成功了 → 清除原有认证故障
        }

    try:
        if existing:
            db.update_account(existing["id"], {
                **fields,
                **({"password_enc": encrypt_secret(password)} if password else {}),
                **session,
            })
            account_id = existing["id"]
        else:
            created = db.insert_account({
                "user_id": user["id"], "csu_username": csu_username,
                "password_enc": encrypt_secret(password), **fields, **session, "created_at": _now_iso(),
            })
            account_id = created["id"]
    except Exception as error:
        if "UNIQUE constraint failed" in str(error):
            raise AppError("该学号已经添加过了", status=409, expose=True) from error
        raise

    return {"account_id": account_id, "verify": {"ok": True, **(probe or {})}}


def update(user: dict, account_id: int, payload: dict) -> dict:
    account = db.get_account(user["id"], account_id)
    if not account:
        raise AppError("账号不存在", status=404, expose=True)

    fields: dict = {"updated_at": _now_iso()}
    try:
        if payload.get("enabled") is not None:
            fields["enabled"] = 1 if payload["enabled"] else 0
        if payload.get("windowStart"):
            fields["window_start"] = str(payload["windowStart"])
        if payload.get("windowEnd"):
            fields["window_end"] = str(payload["windowEnd"])
        # float('abc') 会抛异常（Node 的 Number('abc') 是 NaN），所以解析要包起来
        if payload.get("jd") is not None:
            fields["jd"] = float(payload["jd"])
        if payload.get("wd") is not None:
            fields["wd"] = float(payload["wd"])
        validate_coordinates(fields.get("jd", account["jd"]), fields.get("wd", account["wd"]))
        validate_window(str(fields.get("window_start", account["window_start"])),
                        str(fields.get("window_end", account["window_end"])), cfg.config.max_window_hours)
    except (TypeError, ValueError) as error:
        raise BadRequestError("经纬度必须是有效数字", "invalid_coord") from error

    # 楼栋名是学校按坐标返回的：坐标换了就不该再显示旧的（否则"新坐标 + 旧楼栋"很误导）
    coords_changed = (
        ("jd" in fields and fields["jd"] != account["jd"])
        or ("wd" in fields and fields["wd"] != account["wd"])
    )
    if coords_changed and account["dkdz"]:
        fields["dkdz"] = ""

    # 换密码同样"提交即验证"
    if payload.get("password"):
        password = str(payload["password"])
        try:
            probe = _probe(account["csu_username"], password, account,
                           fields.get("jd", account["jd"]), fields.get("wd", account["wd"]))
        except AppError as error:
            mark_auth_failure(account["id"], error.__cause__ or error, error.message)
            raise
        fields["password_enc"] = encrypt_secret(password)
        fields["needs_reauth"] = 0
        fields["auth_error"] = ""       # 这次验证真的登录成功了 → 清除原有认证故障
        if probe.get("session", {}).get("token"):
            fields.update({
                "token": probe["session"]["token"], "casual": probe["session"]["casual"],
                "cookies": probe["session"]["cookies"], "token_at": _now_iso(),
            })
        # 有学校给的新地址就用它（坐标换了更是必须换）；没给才留旧的
        if probe.get("address") and (coords_changed or not account["dkdz"]):
            fields["dkdz"] = probe["address"]
        if not fields.get("window_start") and probe.get("window"):
            fields["window_start"], fields["window_end"] = probe["window"][0], probe["window"][1]

    db.update_account(account_id, fields)
    return {"account_id": account_id}
