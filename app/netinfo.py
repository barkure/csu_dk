"""请求来源判定。代理头信任边界统一在入口 Uvicorn 一层（ProxyHeadersMiddleware）。"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from . import config as cfg

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


def proxy_startup_options() -> dict:
    """仅在开关开启且配置了可信代理时，让 Uvicorn 解析代理头。"""
    proxies = cfg.config.trusted_proxies
    if not cfg.config.trust_proxy or not proxies:
        return {"proxy_headers": False}
    return {"proxy_headers": True, "forwarded_allow_ips": ",".join(proxies)}


def client_ip(request: Request) -> str:
    """校验 Uvicorn 给出的来源 IP，非法值归入同一个限流键。"""
    raw = str(request.client.host if request.client else "").strip()
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    return raw


def _hostname(request: Request) -> str:
    """Host 头的主机名；"[::1]:8443" 形态取括号内的 IPv6。"""
    raw = (request.headers.get("host") or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else ""
    return raw.split(":")[0]


def is_local_request(request: Request) -> bool:
    """判断请求是否来自本机；Host 缺失一律拒绝（fail closed）。"""
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    if _hostname(request) not in LOCAL_HOSTS:
        return False
    return (request.client.host if request.client else "") in LOOPBACK
