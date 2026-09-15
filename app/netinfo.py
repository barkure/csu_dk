"""请求来源判定。"""
from __future__ import annotations

from fastapi import Request

from . import config as cfg

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def client_ip(request: Request) -> str:
    """仅信任代理提供的来源地址。"""
    if cfg.config.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hostname(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].strip("[]").lower()


def is_local_request(request: Request) -> bool:
    """判断请求是否来自本机。"""
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    host = _hostname(request)
    if host and host not in LOCAL_HOSTS:
        return False
    return (request.client.host if request.client else "") in LOOPBACK
