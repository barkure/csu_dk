"""请求来源判定（限流与本机调试回显都要用）。"""
from __future__ import annotations

from fastapi import Request

from . import config as cfg

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def client_ip(request: Request) -> str:
    """只在明确配置 trust_proxy 时才信 x-forwarded-for（默认它完全由客户端控制）。"""
    if cfg.config.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_local_request(request: Request) -> bool:
    """本机判定要过三道关卡：带代理头 → 不算；Host 非本机 → 不算；最后才信 socket。"""
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
    if host and host not in LOCAL_HOSTS:
        return False
    return (request.client.host if request.client else "") in LOOPBACK
